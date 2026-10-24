"""Extract citation items from tool results and inject their ids."""
from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from core.llm.citations.allocator import AnchorAllocator
from core.llm.citations import anchor_state
from core.llm.citations import types

def first_str(item: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        val = item.get(key)
        if isinstance(val, (str, int, float)) and str(val).strip():
            return str(val)
    return ""


def normalize_citation_url(raw: str) -> str:
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


def dig(data: Dict[str, Any], path: Sequence[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def display_title(tool_name: str, spec: Optional[types.CitationSpec]) -> str:
    if spec and spec.title:
        return spec.title
    return f"{tool_name} 结果"


def item_citation(
    aid: str,
    tool_name: str,
    tool_id: Optional[str],
    spec: types.CitationSpec,
    item: Dict[str, Any],
    index: int,
) -> types.AnchorCitation:
    title = first_str(item, spec.title_keys)[:anchor_state.TITLE_MAX] or display_title(tool_name, spec)
    if spec.snippet_join:
        parts = [first_str(item, (k,)) for k in spec.snippet_join]
        snippet = " · ".join(p for p in parts if p)
    else:
        snippet = first_str(item, spec.snippet_keys)
    return types.AnchorCitation(
        id=aid,
        tool_name=tool_name,
        tool_id=tool_id,
        title=title,
        url=normalize_citation_url(first_str(item, spec.url_keys)),
        snippet=snippet[:anchor_state.SNIPPET_MAX],
        source_type=spec.source_type,
        item_index=index,
    )


def looks_like_error(data: Dict[str, Any]) -> bool:
    if not data.get("error"):
        return False
    # 列表字段仍有内容时不算整体失败（部分工具 error+items 并存）
    for key in anchor_state.LIST_FIELD_ALIASES:
        val = data.get(key)
        if isinstance(val, list) and val:
            return False
    return True


def heuristic_list_field(data: Dict[str, Any]) -> Optional[Tuple[Tuple[str, ...], List[Any]]]:
    """启发式找列表字段：别名优先；否则要求「唯一」的字典数组字段。"""
    scopes: List[Tuple[Tuple[str, ...], Dict[str, Any]]] = [((), data)]
    inner = data.get("result")
    if isinstance(inner, dict):
        scopes.append((("result",), inner))
    for prefix, scope in scopes:
        for alias in anchor_state.LIST_FIELD_ALIASES:
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


def extract_and_inject(
    tool_name: str,
    tool_id: Optional[str],
    data: Dict[str, Any],
    allocator: AnchorAllocator,
) -> List[types.AnchorCitation]:
    """在 data 上就地注入 cite_id，返回登记的引用条目。"""
    spec = anchor_state.TOOL_SPECS.get(tool_name)

    # L1：工具自声明 __citations__（优先级最高，条目顺序即 item_index）
    declared = data.get("__citations__")
    if isinstance(declared, list) and declared and all(isinstance(x, dict) for x in declared):
        base = spec or types.CitationSpec(source_type="unknown")
        out: List[types.AnchorCitation] = []
        for idx, entry in enumerate(declared):
            aid = allocator.next_id()
            entry["cite_id"] = aid
            out.append(
                types.AnchorCitation(
                    id=aid,
                    tool_name=tool_name,
                    tool_id=tool_id,
                    title=str(entry.get("title") or display_title(tool_name, spec))[:anchor_state.TITLE_MAX],
                    url=normalize_citation_url(str(entry.get("url") or "")),
                    snippet=str(entry.get("snippet") or "")[:anchor_state.SNIPPET_MAX],
                    source_type=str(entry.get("source_type") or base.source_type),
                    item_index=idx,
                )
            )
        return out

    if looks_like_error(data):
        return []

    # L2：配置映射（列表型）
    if spec and spec.mode == "list":
        out = []
        for path in spec.items_paths:
            items = dig(data, path)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("error"):
                    continue
                aid = allocator.next_id()
                item["cite_id"] = aid
                out.append(item_citation(aid, tool_name, tool_id, spec, item, len(out)))
        return out

    # L3：无 spec → 启发式找唯一字典数组
    if spec is None:
        found = heuristic_list_field(data)
        if found is not None:
            _path, items = found
            generic = types.CitationSpec(mode="list", source_type="unknown")
            out = []
            for item in items:
                if item.get("error"):
                    continue
                aid = allocator.next_id()
                item["cite_id"] = aid
                out.append(item_citation(aid, tool_name, tool_id, generic, item, len(out)))
            if out:
                return out

    # L4：整份 1 条（显式 whole spec 或彻底认不出）
    aid = allocator.next_id()
    data["cite_id"] = aid
    snippet = json.dumps(data, ensure_ascii=False)[:anchor_state.WHOLE_SNIPPET_MAX]
    return [
        types.AnchorCitation(
            id=aid,
            tool_name=tool_name,
            tool_id=tool_id,
            title=display_title(tool_name, spec),
            url=normalize_citation_url(first_str(data, (spec or types.CitationSpec()).url_keys)),
            snippet=snippet,
            source_type=(spec.source_type if spec else "unknown"),
            item_index=-1,
        )
    ]
