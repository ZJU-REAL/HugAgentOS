"""Typed AgentScope state at the framework boundary."""
from typing import List
from agentscope.state import AgentState
from pydantic import ConfigDict, Field

def _cyfunc_probe():
    return None

_CYFUNCTION_TYPE = type(_cyfunc_probe)

class AgentRuntimeState(AgentState):
    """Extends AgentState to carry the runtime fields of the former ModelContext (replaces agent._jx_context)."""

    model_config = ConfigDict(ignored_types=(_CYFUNCTION_TYPE,))

    model_name: str = ""
    model_provider_id: str = ""
    # When a subagent explicitly configures a model (model_provider_id) → the factory sets this
    # True, and The model selection hook must not override it with the main model based on chat_mode
    # (otherwise the subagent's own model is effectively useless — neither channels nor web get it).
    model_pinned: bool = False
    user_id: str | None = None
    chat_id: str | None = None
    run_id: str | None = None
    capability_scope: str = ""
    journal_owner: str | None = None
    tool_effect_links: dict = Field(default_factory=dict)
    enable_thinking: bool = True
    chat_mode: str | None = None
    uploaded_files: List[dict] = Field(default_factory=list)
    historical_files: List[dict] = Field(default_factory=list)
    user_message_text: str = ""
    # Vision evidence transcribed ahead of the model call by the streaming entry
    # point; The attachment hook injects it verbatim instead of redoing the
    # network round-trip. Empty = transcribe inline (non-streaming callers).
    vision_evidence_text: str = ""
    ontology_enabled: bool = False
    ontology_runtime: dict = Field(default_factory=dict)
    # Set by The steering hook immediately before the next reasoning round.  The
    # streaming adapter turns it into one ``steer_applied`` event, then clears
    # it. Keeping this on the typed runtime state avoids process-global queues.
    steer_delivery: dict | None = None
    # Model responses recorded by the hook adapter the moment they complete,
    # drained by the streaming adapter into ``model_step`` events. Each entry
    # is one ``core.llm.runtime.model_steps`` assistant record.
    pending_model_steps: List[dict] = Field(default_factory=list)
    pending_mcp_progress: List[dict] = Field(default_factory=list)

    def apply_request_context(self, context: dict, user_message_text: str) -> None:
        """Populate per-request runtime fields from the request ``context`` dict (replaces the 1.x agent._jx_context).

        Shared by both entry points — streaming and workflow (non-streaming) — to keep two
        hand-copied field mappings from silently drifting (streaming once didn't lowercase
        chat_mode while workflow did).
        """
        # A child may use the shared role or its own provider. Request context
        # still describes the parent selection; it must not relabel a pinned
        # child (vision routing and audit read these fields).
        if not self.model_pinned:
            self.model_name = str(context.get("model_name", "") or "")
            self.model_provider_id = str(context.get("model_provider_id", "") or "")
        self.user_id = str(context.get("user_id", "") or "") or None
        self.chat_id = str(context.get("chat_id", "") or "") or None
        self.run_id = str(context.get("run_id", "") or "") or None
        self.journal_owner = str(context.get("journal_owner", "") or "") or None
        self.enable_thinking = bool(context.get("enable_thinking", True))
        cm = str(context.get("chat_mode") or "").lower() or None
        if cm:
            self.chat_mode = cm
        self.uploaded_files = list(context.get("uploaded_files", []) or [])
        self.historical_files = list(context.get("historical_files", []) or [])
        self.user_message_text = user_message_text or ""
        self.ontology_enabled = bool(context.get("ontology_enabled", False))
        runtime = context.get("ontology_runtime")
        self.ontology_runtime = runtime if isinstance(runtime, dict) else {}
