"""The manifest and AgentScope request must consume one frozen surface."""

from __future__ import annotations

import pytest
from agentscope.message import UserMsg
from agentscope.tool import FunctionTool, ToolChunk
from core.llm.context.rendering import next_request_sequence, render_context_item
from core.llm.context.ir_constants import KIND_USER_INPUT, POLICY_NEVER
from core.llm.context.items import make_text_context_item
from core.models.constants import IMAGE_TOKEN_RESERVE
from core.llm.runtime.manifest_agent import ManifestBoundAgent
from core.llm.runtime.message_compat import session_to_msgs
from core.llm.tools.ontology_toolkit import OntologyFilteredToolkit
from tests.llm.test_execution_surface_snapshot import CaptureModel


@pytest.mark.asyncio
async def test_public_reply_does_not_invent_message_budget_when_window_is_consumed():
    model = CaptureModel()
    model.context_size = 5

    def oversized_tool() -> ToolChunk:
        """A deliberately oversized schema reserve."""
        return ToolChunk(content=[])

    toolkit = OntologyFilteredToolkit(
        tools=[FunctionTool(oversized_tool, description="schema" * 200)]
    )
    agent = ManifestBoundAgent(
        name="zero-budget-test",
        system_prompt="rules",
        model=model,
        toolkit=toolkit,
    )

    from core.llm.context.safety import ContextBudgetExceeded

    with pytest.raises(ContextBudgetExceeded):
        await agent.reply(UserMsg(name="user", content="current"))
    assert model.calls == []

    assert agent.request_context_manifest["total_budget"] == 0
    assert agent.request_context_manifest["budget_details"]["message_budget"] == 0


@pytest.mark.asyncio
async def test_public_multimodal_request_keeps_or_drops_each_image_whole(monkeypatch):
    from core.llm.context import attachment_hook as mw
    from core.llm.runtime.state import AgentRuntimeState
    from tests.orchestration.hook_support import make_adapter

    monkeypatch.setattr(
        mw.file_context,
        "build_file_context",
        lambda files, user_id=None: "TEXT-FILE-EVIDENCE",
    )
    monkeypatch.setattr(mw, "supports_native_images", lambda state: True)
    monkeypatch.setattr(
        mw.file_context,
        "fetch_image_base64",
        lambda file, user_id=None: ("A" * 4_000, "image/png"),
    )
    model = CaptureModel()
    # Room for one image reserve and the surrounding text, never for two.
    model.context_size = IMAGE_TOKEN_RESERVE + 376
    agent = ManifestBoundAgent(
        name="image-budget-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
        middlewares=[make_adapter(mw.attachment_context_hook())],
        state=AgentRuntimeState(
            uploaded_files=[
                {"file_id": "doc", "name": "notes.txt", "mime_type": "text/plain"},
                {"file_id": "one", "name": "one.png", "mime_type": "image/png"},
                {"file_id": "two", "name": "two.png", "mime_type": "image/png"},
            ],
            historical_files=[],
            user_id="user-1",
        ),
    )
    agent.state.context.extend(
        session_to_msgs(
            [
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
            ]
        )
    )
    request_seq = next_request_sequence(agent.state.context)
    current = render_context_item(
        make_text_context_item(
            "inspect the images",
            item_id=f"request:{request_seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            created_seq=request_seq,
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
        )
    )

    await agent.reply(current)

    blocks = [block for message in model.calls[0]["messages"] for block in message.content]
    data_blocks = [
        block
        for block in blocks
        if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "data"
    ]
    assert len(data_blocks) == 1
    data_payload = (
        data_blocks[0]
        if isinstance(data_blocks[0], dict)
        else data_blocks[0].model_dump(mode="json")
    )
    assert data_payload["source"]["data"] == "A" * 4_000
    assert not any(isinstance(block, str) and '"type":"data"' in block for block in blocks)
    assert model.calls[0]["messages"][-1].get_text_content() == "inspect the images"
    assert any(
        "TEXT-FILE-EVIDENCE" in message.get_text_content()
        for message in model.calls[0]["messages"][:-1]
    )
    manifest = agent.request_context_manifest
    attachment_entries = [
        entry
        for section in ("included", "excluded")
        for entry in manifest[section]
        if entry["kind"] == "attachment"
    ]
    assert len(attachment_entries) == 4  # file evidence + image prefix + two atomic images
    assert sum(entry["action"] == "excluded" for entry in attachment_entries) == 1
