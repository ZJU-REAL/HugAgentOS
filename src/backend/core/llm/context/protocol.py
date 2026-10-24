"""protocol responsibilities."""
from __future__ import annotations
from typing import Any, Iterable, Mapping, Protocol, Sequence
from core.llm.context.items import ContextItem

class ContextAdapterProtocol(Protocol):
    """Framework-neutral seam for turning transport rows into canonical IR."""

    def items_from_messages(
        self,
        messages: Sequence[Any],
        *,
        summary_text: Any = None,
        promote_latest_user: bool = True,
    ) -> list["ContextItem"]: ...

    def messages_from_items(self, items: Iterable["ContextItem"]) -> list[Any]: ...

    def reference_items_from_execution_manifest(
        self, manifest: Any
    ) -> list["ContextItem"]: ...

    def items_from_provider_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> list["ContextItem"]: ...

    def provider_messages_from_items(
        self,
        items: Iterable["ContextItem"],
    ) -> list[Mapping[str, Any]]: ...
