"""slug 规范化与句柄代理。

**slug 格式**：``<type>/<name>``，如 ``entity/shili-keji``、``concept/rag``、
``summary/<document_id>``。小写、连字符分隔，库内唯一。

**句柄代理**：文档摘要页的 slug 里带 64 位文档 ID，高熵字符串让模型抄错的概率
很高（抄错一位就是一条死链）。送进模型前把它换成 ``ref-1`` 这样的短句柄，模型
输出 ``[[ref-1|标题]]`` 之后再还原成真 slug。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List

# slug 里保留的字符：ASCII 字母数字与连字符
_SLUG_ALLOWED = re.compile(r"[^a-z0-9\-]+")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_MAX_SLUG_NAME = 120

VALID_SLUG_PREFIXES = ("entity", "concept", "summary", "index", "synthesis", "comparison")


def slugify_name(name: str) -> str:
    """把展示名压成 slug 的名字段。

    只做 ASCII 归一：非拉丁文名的罗马化由模型在抽取阶段完成（提示词已要求拼音
    形式）。这里兜底的是模型仍然给了中文的情况——退化成十六进制指纹，保证唯一、
    可用，只是不好看。slug 是标识符不是展示名，可读性由 title 承担。
    """
    text = unicodedata.normalize("NFKD", str(name or "")).strip().lower()
    text = text.replace("_", "-").replace(" ", "-").replace("/", "-")
    ascii_text = _SLUG_ALLOWED.sub("-", text)
    ascii_text = _MULTI_HYPHEN.sub("-", ascii_text).strip("-")
    if ascii_text:
        return ascii_text[:_MAX_SLUG_NAME]
    fingerprint = abs(hash(str(name or ""))) % (16**8)
    return f"item-{fingerprint:08x}"


def normalize_slug(raw: str, *, default_type: str = "concept") -> str:
    """把模型给的 slug 归一成 ``<type>/<name>``。

    模型有时只给名字不给前缀、有时给全角斜杠、有时多加一层路径——统统收敛成
    两段式，避免同一事物因为写法差异分裂成多个页面。
    """
    text = str(raw or "").strip().replace("／", "/").strip("/")
    if not text:
        return ""
    parts = [p for p in text.split("/") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        page_type = default_type
        name = parts[0]
    else:
        head = parts[0].strip().lower()
        if head in VALID_SLUG_PREFIXES:
            page_type = head
            name = "-".join(parts[1:])
        else:
            page_type = default_type
            name = "-".join(parts)
    # summary 页的名字段是文档 ID，原样保留（大小写敏感、不能被 slugify 改写）
    if page_type == "summary":
        return f"summary/{name.strip()}"
    slug_name = slugify_name(name)
    return f"{page_type}/{slug_name}" if slug_name else ""


def summary_slug(document_id: str) -> str:
    return f"summary/{str(document_id).strip()}"


def readable_from_slug(slug: str) -> str:
    """从 slug 尾段推一个可读名，用于标题解析失败时的兜底展示。"""
    tail = str(slug or "").split("/", 1)[-1]
    return tail.replace("-", " ").strip() or str(slug or "")


class SlugHandles:
    """真 slug ↔ 短句柄的双向映射。

    用法：``render`` 得到给模型看的句柄清单，模型输出后用 ``restore`` 把正文里的
    句柄换回真 slug。未在映射里的句柄保持原样，由下游的死链清理兜住。
    """

    def __init__(self, slugs: Iterable[str]) -> None:
        self._to_handle: Dict[str, str] = {}
        self._to_slug: Dict[str, str] = {}
        for index, slug in enumerate(dict.fromkeys(s for s in slugs if s), start=1):
            handle = f"ref-{index}"
            self._to_handle[slug] = handle
            self._to_slug[handle] = slug

    def handle_for(self, slug: str) -> str:
        return self._to_handle.get(slug, slug)

    def restore(self, text: str) -> str:
        """把正文里的 ``[[ref-N|名称]]`` 还原成 ``[[真 slug|名称]]``。"""
        if not text or not self._to_slug:
            return text

        def _replace(match: re.Match) -> str:
            target = match.group(1).strip()
            label = match.group(2)
            resolved = self._to_slug.get(target)
            if not resolved:
                return match.group(0)
            return f"[[{resolved}|{label}]]" if label else f"[[{resolved}]]"

        return _WIKI_LINK_RE.sub(_replace, text)


_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")


def extract_link_slugs(content: str) -> List[str]:
    """取出正文里所有 ``[[slug|名称]]`` 的 slug，保序去重。"""
    seen: List[str] = []
    for match in _WIKI_LINK_RE.finditer(content or ""):
        slug = match.group(1).strip()
        if slug and slug not in seen:
            seen.append(slug)
    return seen


def strip_links_except(content: str, allowed: Iterable[str], *, self_slug: str = "") -> str:
    """把不在白名单里的 Wiki 链接降级成纯文本。

    模型即便被反复叮嘱也会臆造 slug 或链到自己，落库前必须清一遍，否则 Wiki 里
    会积累一堆点开就 404 的死链。
    """
    allowed_set = {s for s in allowed if s}

    def _replace(match: re.Match) -> str:
        slug = match.group(1).strip()
        label = (match.group(2) or "").strip()
        if slug in allowed_set and slug != self_slug:
            return match.group(0)
        return label or readable_from_slug(slug)

    return _WIKI_LINK_RE.sub(_replace, content or "")
