"""列表查询的统一分页。

页长默认值、页码归一化和"不分页"的语义只在这里定义一次，仓储层、REST 列表接口和
给模型用的列表工具共用，避免同一个语义在各处各写一个数字（历史上我的空间列表就因此
被一个写死的 100 条上限截断，翻不到后面的文件）。
"""

from __future__ import annotations

from typing import Any, Optional

#: 调用方没给页长时用多少条一页。没有上限：页长由调用方决定。
DEFAULT_PAGE_SIZE = 20


def normalize_page(page: Any) -> int:
    """页码归一化为 >= 1 的整数。"""
    try:
        return max(int(page), 1)
    except (TypeError, ValueError):
        return 1


def normalize_page_size(page_size: Any) -> Optional[int]:
    """页长归一化；``None`` / 非正数 / 解析不了都表示不分页（取全部）。"""
    if page_size is None:
        return None
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def apply_page(query: Any, *, page: Any = 1, page_size: Any = DEFAULT_PAGE_SIZE) -> Any:
    """把页码/页长套到 SQLAlchemy 查询上；页长为空即不加 offset/limit。"""
    size = normalize_page_size(page_size)
    if size is None:
        return query
    return query.offset((normalize_page(page) - 1) * size).limit(size)
