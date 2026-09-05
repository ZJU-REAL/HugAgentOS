"""AgentScope adapter that pins one execution surface per model request."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from agentscope.agent import Agent

from core.llm.context_adapter import AgentScopeContextAdapter
from core.llm.context_ir import (
    VISIBILITY_MANIFEST_ONLY,
    ContextAssembler,
    estimate_context_tokens,
)
from core.llm.context_manager import FALLBACK_CONTEXT_WINDOW, usable_context_window


class ManifestBoundAgent(Agent):
    """Ensure AgentScope and the execution manifest consume one snapshot."""

    def __init__(
        self, *args, context_adapter_factory=None, **kwargs
    ) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._context_adapter_factory = context_adapter_factory or AgentScopeContextAdapter
        self._tool_reserve_memo: Optional[tuple[int, int]] = None

    @property
    def context_adapter_factory(self):  # noqa: ANN201
        return self._context_adapter_factory

    async def compress_context(self, context_config=None) -> None:  # noqa: ANN001
        """Disable AgentScope's second, opaque context selector.

        Durable pre-turn compaction may replace history with an explicit
        checkpoint before it reaches the agent.  At request time the canonical
        assembler below is the sole budget authority and records every
        candidate decision.  Running AgentScope compression here would delete
        candidates before the final manifest and make exclusions unauditable.
        """
        del context_config

    @property
    def request_context_manifest(self) -> dict[str, Any]:
        assembly = getattr(self, "_jx_context_assembly", None)
        return assembly.manifest if assembly is not None else {}

    @property
    def execution_manifest(self) -> Any:
        return getattr(self, "_jx_execution_manifest", None)

    @property
    def evidence_bundle(self) -> Any:
        return getattr(self, "_jx_asset_bundle", None)

    def bind_execution_surface(self, manifest: Any, *, bundle: Any = None) -> None:
        """Publish one frozen surface through the agent's public evidence seam."""
        self._jx_base_execution_manifest = manifest
        self._jx_execution_manifest = manifest
        self._jx_asset_bundle = bundle

    def set_context_manifest_listener(
        self,
        listener: Optional[Callable[[Any], Any]],
    ) -> None:
        self._jx_context_manifest_listener = listener

    def bind_request_evidence(self, manifest: Any, *, bundle: Any) -> None:
        """Publish evidence for the exact post-budget model request."""
        self._jx_execution_manifest = manifest
        self._jx_asset_bundle = bundle

    def clear_request_evidence(self, manifest: Any) -> None:
        """Keep the manifest observable but fail closed on its unavailable bundle."""
        self._jx_execution_manifest = manifest
        self._jx_asset_bundle = None

    async def _publish_context_assembly(self, assembly: Any, base_manifest: Any) -> None:
        """Publish one final assembly through the same evidence listener."""
        self._jx_context_assembly = assembly
        self._jx_context_manifest = assembly.manifest
        if base_manifest is None:
            return
        request_manifest = base_manifest.with_context_manifest(assembly.manifest)
        self._jx_execution_manifest = request_manifest
        listener = getattr(self, "_jx_context_manifest_listener", None)
        if listener is not None:
            result = listener(request_manifest)
            if inspect.isawaitable(result):
                await result

    async def _rebind_provider_context(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Bind the exact post-media-recovery retry before it reaches the wire."""
        adapter = self._context_adapter_factory()
        items = adapter.items_from_provider_messages(messages)
        base_manifest = getattr(
            self,
            "_jx_base_execution_manifest",
            getattr(self, "_jx_execution_manifest", None),
        )
        if base_manifest is not None:
            # Provider rows already contain every model-visible prompt section,
            # including live project material.  Re-add only evidence-only refs;
            # injecting the live project item here would duplicate it on retry.
            items.extend(
                item
                for item in adapter.reference_items_from_execution_manifest(base_manifest)
                if item.visibility == VISIBILITY_MANIFEST_ONLY
            )
        assembly = ContextAssembler(
            total_budget=int(getattr(self, "_jx_context_budget", 0)),
            budget_details=getattr(self, "_jx_context_budget_details", {}),
        ).assemble(items)
        await self._publish_context_assembly(assembly, base_manifest)
        return adapter.provider_messages_from_items(assembly.included)

    def _tool_reserve_tokens(self, snapshot: Any, tools: Any) -> int:
        """Token cost of the frozen tool schemas, recomputed only when they change.

        The estimate walks and re-serializes every schema — milliseconds per
        call on a large tool surface, and this runs on every model call in the
        ReAct loop. The surface is content-addressed by ``generation``, and the
        tools AgentScope hands us come from the same frozen enumeration, so the
        generation is an exact cache key rather than a heuristic.
        """
        generation = getattr(snapshot, "generation", None)
        if generation is None:
            return estimate_context_tokens(tools)
        memo = self._tool_reserve_memo
        if memo is not None and memo[0] == generation:
            return memo[1]
        reserve = estimate_context_tokens(tools)
        self._tool_reserve_memo = (generation, reserve)
        return reserve

    async def _prepare_model_input(self):  # noqa: ANN202
        snapshot = None
        freeze = getattr(self.toolkit, "freeze_execution_surface", None)
        if freeze is not None:
            groups = self.state.tool_context.activated_groups
            snapshot = await freeze(groups)
        model_input = await super()._prepare_model_input()

        adapter = self._context_adapter_factory()
        items = adapter.items_from_messages(
            model_input["messages"],
            summary_text=self.state.summary,
        )
        base_manifest = getattr(
            self,
            "_jx_base_execution_manifest",
            getattr(self, "_jx_execution_manifest", None),
        )
        if base_manifest is not None:
            # Prompt-side material (live project content) belongs to the prefix,
            # behind the system prompt and ahead of the conversation. The
            # assembler emits items in the order it receives them, so place them
            # here instead of appending and relying on a sort to hoist them back.
            references = adapter.reference_items_from_execution_manifest(base_manifest)
            if references:
                head = 0
                while head < len(items) and items[head].render_role == "system":
                    head += 1
                items[head:head] = references

        context_size = int(getattr(self.model, "context_size", 0) or 0)
        # The usable share already carries the headroom for the response, so the
        # only thing subtracted here is the measured tool schema.
        effective_window = context_size or FALLBACK_CONTEXT_WINDOW
        tool_reserve = self._tool_reserve_tokens(snapshot, model_input.get("tools") or [])
        request_budget = max(0, usable_context_window(effective_window) - tool_reserve)
        budget_details = {
            "context_window": effective_window,
            "tool_reserve_tokens": tool_reserve,
            "message_budget": request_budget,
        }
        assembly = ContextAssembler(
            total_budget=request_budget,
            budget_details=budget_details,
        ).assemble(items)
        model_input["messages"] = adapter.messages_from_items(assembly.included)
        self._jx_context_budget = request_budget
        self._jx_context_budget_details = budget_details
        await self._publish_context_assembly(assembly, base_manifest)
        set_rewrite_listener = getattr(self.model, "set_context_rewrite_listener", None)
        if callable(set_rewrite_listener):
            set_rewrite_listener(self._rebind_provider_context)
        return model_input


__all__ = ["ManifestBoundAgent"]
