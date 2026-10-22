"""把页面标题/别名在正文里自动织成 Wiki 链接。

模型在写页时已经会用 ``[[slug|名称]]``，但漏链很常见。这一层在落库前补一遍：
每个目标页在正文里**只织第一处**安全出现的位置，避免整页被链接刷屏。

"安全"是这里的全部难点——绝不能改到这些区域：

* 围栏代码块（``` 或 ~~~）与行内代码
* 已有的 Markdown 链接文字与 URL、autolink、引用式链接的定义行
* 已经存在的 ``[[...]]`` Wiki 链接
* 标题行（改标题会让目录锚点漂移）

漏掉任何一项都会把正文改坏，而这类损坏很隐蔽——代码片段里多出一对方括号，
读者要很久才发现。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

Span = Tuple[int, int]

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`+")
_MD_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_REF_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\[[^\]\n]*\]")
_REF_DEF_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:.*$", re.MULTILINE)
_AUTOLINK_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9+.\-]*:[^>\s]*>")
_WIKI_LINK_RE = re.compile(r"\[\[[^\[\]]*\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s.*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")

# 太短的表面词不织：单字/双字命中率极低而误伤极高（"中国"出现在"中国式现代化"里）
_MIN_SURFACE_LEN = 2
_MIN_ASCII_SURFACE_LEN = 3


def _fenced_spans(text: str) -> List[Span]:
    """围栏代码块的区间。闭合围栏必须与开栏同符号且不短于它。"""
    spans: List[Span] = []
    pos = 0
    while True:
        match = _FENCE_RE.search(text, pos)
        if not match:
            break
        marker = match.group(2)
        char = marker[0]
        start = match.start()
        body_start = text.find("\n", match.end())
        if body_start == -1:
            spans.append((start, len(text)))
            break
        closing = re.compile(rf"^\s*{re.escape(char)}{{{len(marker)},}}\s*$", re.MULTILINE)
        close = closing.search(text, body_start)
        end = close.end() if close else len(text)
        spans.append((start, end))
        pos = end
    return spans


def _inline_code_spans(text: str, blocked: List[Span]) -> List[Span]:
    """行内代码区间：反引号成对匹配，且两端 run 长度必须相同。"""
    spans: List[Span] = []
    pos = 0
    while True:
        opener = _INLINE_CODE_RE.search(text, pos)
        if not opener:
            break
        if _in_any(opener.start(), blocked):
            pos = opener.end()
            continue
        run = opener.group(0)
        closer = None
        search_pos = opener.end()
        while True:
            candidate = _INLINE_CODE_RE.search(text, search_pos)
            if not candidate:
                break
            if candidate.group(0) == run:
                closer = candidate
                break
            search_pos = candidate.end()
        if not closer:
            break
        spans.append((opener.start(), closer.end()))
        pos = closer.end()
    return spans


def _in_any(index: int, spans: Sequence[Span]) -> bool:
    return any(start <= index < end for start, end in spans)


def _overlaps(span: Span, spans: Sequence[Span]) -> bool:
    start, end = span
    return any(not (end <= s or start >= e) for s, e in spans)


def forbidden_spans(text: str) -> List[Span]:
    """所有不可改写区间，合并排序后返回。"""
    spans = _fenced_spans(text)
    spans.extend(_inline_code_spans(text, spans))
    for pattern in (
        _MD_LINK_RE,
        _REF_LINK_RE,
        _REF_DEF_RE,
        _AUTOLINK_RE,
        _WIKI_LINK_RE,
        _HEADING_RE,
        _URL_RE,
    ):
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    spans.sort()
    merged: List[Span] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _is_ascii_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _has_word_boundary(text: str, start: int, end: int) -> bool:
    """ASCII 表面词要求词边界；CJK 不做边界检查（本来就不分词）。"""
    surface = text[start:end]
    if not surface.isascii():
        return True
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (_is_ascii_word_char(before) or _is_ascii_word_char(after))


def _find_safe_match(
    text: str, surface: str, blocked: Sequence[Span], *, lowered: Optional[str] = None
) -> int:
    """找到 surface 第一处可安全替换的位置；找不到返回 -1。

    ``lowered`` 是 ``text.lower()`` 的缓存——调用方在循环里复用，避免每个候选词
    都把整篇正文重新小写一遍。
    """
    lowered = text.lower() if lowered is None else lowered
    needle = surface.lower()
    pos = 0
    while True:
        index = lowered.find(needle, pos)
        if index == -1:
            return -1
        end = index + len(surface)
        if not _overlaps((index, end), blocked) and _has_word_boundary(text, index, end):
            return index
        pos = index + 1


def linkify_content(
    content: str,
    targets: Dict[str, Sequence[str]],
    *,
    self_slug: str = "",
    max_links_per_page: int = 40,
) -> str:
    """把 ``targets`` 里的表面词在正文中织成 Wiki 链接。

    ``targets`` 形如 ``{slug: [标题, 别名...]}``。每个 slug 最多织一次，长表面词
    优先（先匹配"示例科技有限公司"再匹配"示例科技"，否则短名会把长名切碎）。
    """
    if not content or not targets:
        return content

    candidates: List[Tuple[str, str]] = []
    for slug, surfaces in targets.items():
        if not slug or slug == self_slug:
            continue
        for surface in surfaces:
            text = str(surface or "").strip()
            if not text:
                continue
            min_len = _MIN_ASCII_SURFACE_LEN if text.isascii() else _MIN_SURFACE_LEN
            if len(text) < min_len:
                continue
            candidates.append((text, slug))
    if not candidates:
        return content

    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    result = content
    linked_slugs: set[str] = set()
    # 禁区与小写副本只随「正文真的被改过」而失效。候选数量是全库条目级（可达数千），
    # 而实际插入次数被 max_links_per_page 卡在 40 以内——每个候选都重算一遍
    # 8 条正则全文扫描，是把一次 O(插入数) 的开销放大成 O(候选数)。这段还跑在写页
    # 线程池里抢 GIL，会直接拖慢并发的模型写入。
    blocked = forbidden_spans(result)
    lowered = result.lower()
    for surface, slug in candidates:
        if slug in linked_slugs or len(linked_slugs) >= max_links_per_page:
            continue
        index = _find_safe_match(result, surface, blocked, lowered=lowered)
        if index == -1:
            continue
        matched = result[index : index + len(surface)]
        replacement = f"[[{slug}|{matched}]]"
        result = result[:index] + replacement + result[index + len(surface) :]
        linked_slugs.add(slug)
        blocked = forbidden_spans(result)
        lowered = result.lower()
    return result
